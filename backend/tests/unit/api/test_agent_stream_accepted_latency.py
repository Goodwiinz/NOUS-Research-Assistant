"""The accepted-latency SLI must be measured from request arrival.

``AGENT_STREAM_ACCEPTED_DURATION`` is the SLI for "how long until the client
saw its first server frame". The clock used to be stamped when
``_SeqEmitter`` was constructed — inside the generator, i.e. *after* the route
handler had already spent wall time on rate limiting and response setup. The
one number the SLI exists to catch (pre-stream overhead) was the one number it
excluded, so a rate limiter that got slow would show up as a flat graph.

The test drives the real ``POST /stream`` handler with a rate-limiter check
that takes a measurable amount of time, pulls the first SSE frame, and asserts
the recorded observation covers that delay. With the clock stamped at emitter
construction the observation is ~0 and the test fails.

Note on libpq: ``stream_event_generator`` lazily imports ``psycopg`` inside its
body; this host may have no libpq, so a stub is installed the same way
``test_agent_streaming_revise_flag.py`` does (see nous-libpq-test-env).
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.security import TokenData
from tests.utils.agent_stream import make_stream_request, sse_data, sse_event_name


def _install_psycopg_stub() -> None:
    """Inject a minimal psycopg stub into sys.modules if libpq is absent."""
    if "psycopg" in sys.modules:
        return
    stub = types.ModuleType("psycopg")
    stub.OperationalError = OSError  # type: ignore[attr-defined]
    sys.modules["psycopg"] = stub


_install_psycopg_stub()

# Wall time deliberately burned inside the route handler, before the generator
# (and therefore the emitter) exists. Long enough that the two clock choices
# cannot be confused with scheduler noise.
_PREFLIGHT_DELAY_SECONDS = 0.25


class _Recorder:
    """Stand-in for the prometheus histogram; captures observed seconds."""

    def __init__(self) -> None:
        self.observed: List[float] = []

    def observe(self, value: float) -> None:
        self.observed.append(value)


@pytest.mark.asyncio
async def test_accepted_latency_measured_from_request_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accepted frame's SLI must include pre-stream handler overhead."""
    from fastapi import BackgroundTasks

    from src.api.agent import execute as execute_mod
    from src.services.agent import observability

    recorder = _Recorder()
    monkeypatch.setattr(observability, "_METRICS_AVAILABLE", True)
    monkeypatch.setattr(observability, "AGENT_STREAM_ACCEPTED_DURATION", recorder)

    async def slow_check_rate_limit(*_args: Any, **_kwargs: Any) -> tuple[bool, int]:
        await asyncio.sleep(_PREFLIGHT_DELAY_SECONDS)
        return True, 0

    monkeypatch.setattr(
        execute_mod._agent_rate_limiter, "check_rate_limit", slow_check_rate_limit
    )
    monkeypatch.setattr(
        execute_mod._agent_rate_limiter, "record_attempt", AsyncMock(return_value=None)
    )

    body = make_stream_request(thread_id="11111111-1111-1111-1111-111111111401")
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    current_user = Mock(id="user-1", organization_id="org-1")

    with (
        patch("src.api.agent.streaming.AsyncSessionLocal", return_value=AsyncMock()),
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        response = await execute_mod.stream_agent(
            body,
            request,  # type: ignore[arg-type]
            BackgroundTasks(),
            current_user=current_user,
            # Browser-provenance token: the Codex authority gate only reads
            # ``is_cli``, and this turn is an ordinary NOUS stream anyway.
            token=TokenData(user_id="user-1", organization_id="org-1"),
        )
        body_iterator = response.body_iterator
        first_frame = await body_iterator.__anext__()  # type: ignore[union-attr]
        await body_iterator.aclose()  # type: ignore[union-attr]

    assert sse_event_name(first_frame) == "status"
    assert sse_data(first_frame)["phase"] == "accepted"

    assert recorder.observed, "accepted-latency SLI was never observed"
    assert recorder.observed[0] >= _PREFLIGHT_DELAY_SECONDS, (
        "accepted-latency excludes pre-stream handler overhead: observed "
        f"{recorder.observed[0]:.3f}s for a request that spent "
        f"{_PREFLIGHT_DELAY_SECONDS:.3f}s in rate limiting alone"
    )
