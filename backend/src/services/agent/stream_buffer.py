"""Redis buffer for resumable agent SSE streams.

One Redis list per run (``agent:stream:{stream_id}``) holds JSON
``{seq, frame}`` entries; ``agent:stream:active:{thread_id}`` points at the
currently active run. TTL 3600 matches the job store's default 1h record
TTL, NOT the 24h it keeps ``awaiting_confirmation`` records (audit R2-H3): a
run parked on a confirmation for over an hour has no replayable buffer left.

ponytail: poll-follow over a plain list; swap to pub/sub if replay latency
matters.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from src.services.agent.job_store import get_redis

logger = logging.getLogger(__name__)

# 1h — the job store's default TTL; parked HITL job records outlive it (24h).
_TTL_SECONDS = 3600


@dataclass
class BufferedFrame:
    seq: int
    frame: str


def _parse_frame(item: str | bytes, key: str) -> BufferedFrame | None:
    """Parse one buffer entry, skipping (and logging) corrupt ones.

    A single corrupt list entry must not brick replay for the rest of the
    buffer's TTL — the reader drops it and keeps the surrounding frames
    (audit S-L10); the seq gap stays visible in the warning.
    """
    try:
        frame = BufferedFrame(**json.loads(item))
        # Dataclass annotations are not runtime validation: valid JSON with
        # the wrong field types would pass construction and blow up later in
        # `f.seq > after_seq` — the exact replay-bricking S-L10 closes.
        if not isinstance(frame.seq, int) or not isinstance(frame.frame, str):
            raise TypeError("frame fields have invalid types")
        return frame
    except (ValueError, TypeError) as exc:
        logger.warning(
            "Dropping corrupt stream-buffer entry",
            extra={"buffer_key": key, "error": str(exc)},
        )
        return None


def _buffer_key(stream_id: str) -> str:
    return f"agent:stream:{stream_id}"


def _active_key(thread_id: str) -> str:
    return f"agent:stream:active:{thread_id}"


def _run_key(run_id: str) -> str:
    return f"agent:stream:run:{run_id}"


def _stream_thread_key(stream_id: str) -> str:
    return f"agent:stream:thread:{stream_id}"


async def start_stream(thread_id: str, *, run_id: str | None = None) -> str:
    """Mint a stream id and record its thread/run correlations."""
    stream_id = str(uuid.uuid4())
    redis = await get_redis()
    if redis is not None:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.set(_active_key(thread_id), stream_id, ex=_TTL_SECONDS)
            pipe.set(_stream_thread_key(stream_id), thread_id, ex=_TTL_SECONDS)
            if run_id is not None:
                pipe.set(_run_key(run_id), stream_id, ex=_TTL_SECONDS)
            await pipe.execute()
    return stream_id


async def append(stream_id: str, seq: int, frame: str) -> None:
    """Append one frame to the stream's replay buffer.

    rpush/ltrim/expire pipelined into one round trip (L14): three separate
    awaits let a connection drop between them leave a partially-applied
    key — e.g. rpush lands (creating the key with no TTL) but expire never
    runs, so the key survives forever instead of the intended hour.
    """
    redis = await get_redis()
    if redis is None:
        return
    key = _buffer_key(stream_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.rpush(key, json.dumps({"seq": seq, "frame": frame}))
        # ponytail: cap at 5000 frames; resume past that loses oldest frames —
        # bump or index by seq if real turns exceed it.
        pipe.ltrim(key, -5000, -1)
        pipe.expire(key, _TTL_SECONDS)
        await pipe.execute()


async def read_after(
    stream_id: str, after_seq: int, *, start_index: int | None = None
) -> list[BufferedFrame]:
    """Return buffered frames with seq > after_seq (empty if unknown sid).

    ``start_index`` is an optional hint (L15): a caller that polls the same
    stream repeatedly (``replay_buffered_stream``) can pass how many frames
    it has already consumed, so this can slice straight to the new tail
    instead of re-fetching and re-parsing the whole (up to 5000-frame) buffer
    on every ~1s poll. The hint is only trusted after a cheap probe of the
    item immediately before it: on an unchanged list that item is exactly
    the last frame the caller consumed (seq == after_seq), so an append-only
    list means everything after it is new. If ``append``'s ltrim has trimmed
    the buffer since, every index shifted and the probe won't match — fall
    back to the full scan below rather than risk silently skipping frames.
    """
    redis = await get_redis()
    if redis is None:
        return []
    key = _buffer_key(stream_id)
    if start_index:
        probe_raw = await redis.lrange(key, start_index - 1, -1)
        probe = _parse_frame(probe_raw[0], key) if probe_raw else None
        if probe is not None and probe.seq == after_seq:
            frames = [
                frame
                for item in probe_raw[1:]
                if (frame := _parse_frame(item, key)) is not None
            ]
            return [f for f in frames if f.seq > after_seq]
    raw = await redis.lrange(key, 0, -1)
    frames = [frame for item in raw if (frame := _parse_frame(item, key)) is not None]
    return [f for f in frames if f.seq > after_seq]


async def active_stream_id(thread_id: str) -> str | None:
    """Return the thread's active stream id, if any."""
    redis = await get_redis()
    if redis is None:
        return None
    return await redis.get(_active_key(thread_id))


async def stream_id_for_run(run_id: str) -> str | None:
    """Return the immutable stream attached to a durable run."""
    redis = await get_redis()
    if redis is None:
        return None
    return await redis.get(_run_key(run_id))


async def thread_id_for_stream(stream_id: str) -> str | None:
    """Return the thread that owns a stream id."""
    redis = await get_redis()
    if redis is None:
        return None
    return await redis.get(_stream_thread_key(stream_id))


async def finish_stream(thread_id: str, stream_id: str) -> None:
    """Clear the active pointer only if it still belongs to this run.

    WATCH/MULTI optimistic transaction (same pattern as
    job_store.compare_and_set_status) so a start_stream landing between the
    GET and the DELETE can't have its new pointer clobbered. Single attempt,
    no retry: losing the race means a newer run owns the pointer — give up.
    """
    redis = await get_redis()
    if redis is None:
        return
    from redis.exceptions import WatchError

    key = _active_key(thread_id)
    async with redis.pipeline(transaction=True) as pipe:
        try:
            await pipe.watch(key)
            current = await pipe.get(key)  # immediate mode after WATCH
            if current != stream_id:
                await pipe.reset()
                return
            pipe.multi()
            pipe.delete(key)
            await pipe.execute()  # raises WatchError if key changed
        except WatchError:
            # A newer run re-set the pointer mid-transaction; it owns it.
            await pipe.reset()
