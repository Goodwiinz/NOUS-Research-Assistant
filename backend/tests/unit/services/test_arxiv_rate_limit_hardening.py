"""Budget and response-policy tests for the shared arXiv request boundary."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit


class TestGateCeiling:
    def test_wait_budget_covers_at_least_one_pacing_interval(self) -> None:
        from src.services.arxiv.arxiv_service import (
            _ARXIV_MAX_WAIT_MS,
            _ARXIV_MIN_INTERVAL_MS,
        )

        assert _ARXIV_MAX_WAIT_MS >= _ARXIV_MIN_INTERVAL_MS

    def test_ceiling_stays_inside_the_tool_call_budget(self) -> None:
        """A cap above the tool timeout trades a 429 for a guaranteed timeout."""
        from src.services.agent._nodes_tools import AGENT_LLM_TIMEOUT_SECONDS
        from src.services.arxiv.arxiv_service import _ARXIV_MAX_WAIT_MS

        assert _ARXIV_MAX_WAIT_MS / 1000.0 < AGENT_LLM_TIMEOUT_SECONDS

    def test_lease_outlives_the_longest_gate_wait(self) -> None:
        """A lease must not expire while a caller is still inside the gate."""
        from src.services.arxiv.arxiv_service import (
            _ARXIV_GATE_TTL_MS,
            _ARXIV_MAX_WAIT_MS,
        )

        assert _ARXIV_GATE_TTL_MS > _ARXIV_MAX_WAIT_MS

    def test_lease_outlives_pacing_and_the_absolute_request_deadline(self) -> None:
        """Per-operation HTTP timeouts alone do not bound a slow response."""
        from src.services.arxiv.arxiv_service import (
            _ARXIV_GATE_TTL_MS,
            _ARXIV_MAX_REQUEST_SECONDS,
            _ARXIV_MIN_INTERVAL_MS,
        )

        occupied_ms = _ARXIV_MIN_INTERVAL_MS + int(_ARXIV_MAX_REQUEST_SECONDS * 1000)
        assert _ARXIV_GATE_TTL_MS > occupied_ms

    def test_406_cooldown_is_bounded(self) -> None:
        from src.services.arxiv.arxiv_service import _ARXIV_406_COOLDOWN_MS

        assert 3_000 <= _ARXIV_406_COOLDOWN_MS <= 300_000


class TestRetryAfter:
    def test_delta_seconds_header_is_honoured(self) -> None:
        from src.services.arxiv.arxiv_service import _retry_after_seconds

        assert _retry_after_seconds({"Retry-After": "10"}, default=3.0) == 10.0

    def test_case_insensitive(self) -> None:
        from src.services.arxiv.arxiv_service import _retry_after_seconds

        assert _retry_after_seconds({"retry-after": "7"}, default=3.0) == 7.0

    def test_large_value_is_honoured_in_full(self) -> None:
        """Retry-After is upstream's explicit not-before instruction."""
        from src.services.arxiv.arxiv_service import _retry_after_seconds

        assert _retry_after_seconds({"Retry-After": "60"}, default=3.0) == 60.0

    def test_absent_header_falls_back_to_the_default(self) -> None:
        from src.services.arxiv.arxiv_service import _retry_after_seconds

        assert _retry_after_seconds({}, default=3.0) == 3.0

    def test_http_date_form_is_honoured(self, monkeypatch) -> None:
        from datetime import datetime, timezone

        import src.services.arxiv.arxiv_service as svc
        from src.services.arxiv.arxiv_service import _retry_after_seconds

        now = datetime(2026, 10, 21, 7, 27, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(svc, "_utcnow", lambda: now)
        header = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        assert _retry_after_seconds(header, default=3.0) == 60.0

    def test_garbage_header_falls_back(self) -> None:
        from src.services.arxiv.arxiv_service import _retry_after_seconds

        assert _retry_after_seconds({"Retry-After": "soon"}, default=3.0) == 3.0

    def test_negative_is_floored_at_zero(self) -> None:
        from src.services.arxiv.arxiv_service import _retry_after_seconds

        assert _retry_after_seconds({"Retry-After": "-5"}, default=3.0) == 0.0


class _Resp:
    def __init__(self, status_code: int, *, text: str = "", retry_after=None):
        self.status_code = status_code
        self.text = text
        self.headers = {}
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


class _Client:
    responses: list[_Resp] = []

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        return self.responses.pop(0)


class _SlowClient(_Client):
    async def get(self, *_args, **_kwargs):
        await asyncio.Event().wait()


async def test_406_arms_cooldown_and_is_not_retried(monkeypatch) -> None:
    import src.services.arxiv.arxiv_service as svc

    _Client.responses = [_Resp(406)]
    acquire = AsyncMock(return_value=0.0)
    arm = AsyncMock()
    monkeypatch.setattr(svc, "_acquire_arxiv_rate_slot", acquire)
    monkeypatch.setattr(svc, "_arm_arxiv_cooldown", arm)

    with pytest.raises(svc.ArxivUpstreamUnavailableError):
        await svc.request_arxiv_api(
            svc.ArXivIngestionService.ARXIV_API_BASE,
            {"search_query": "all:test"},
            client_factory=_Client,
        )

    assert acquire.await_count == 1
    arm.assert_awaited_once_with(
        svc._ARXIV_406_COOLDOWN_KEY,
        svc._ARXIV_406_COOLDOWN_MS / 1000.0,
        "upstream-unavailable",
    )


async def test_coordination_failure_while_arming_cooldown_is_not_retried(
    monkeypatch,
) -> None:
    import src.services.arxiv.arxiv_service as svc

    _Client.responses = [_Resp(406)]
    acquire = AsyncMock(return_value=0.0)
    monkeypatch.setattr(svc, "_acquire_arxiv_rate_slot", acquire)
    monkeypatch.setattr(
        svc,
        "_arm_arxiv_cooldown",
        AsyncMock(side_effect=svc.ArxivCoordinationUnavailableError("redis down")),
    )

    with pytest.raises(svc.ArxivCoordinationUnavailableError):
        await svc.request_arxiv_api(
            svc.ArXivIngestionService.ARXIV_API_BASE,
            {"search_query": "all:test"},
            client_factory=_Client,
        )

    assert acquire.await_count == 1


async def test_absolute_deadline_bounds_a_slow_streaming_response(monkeypatch) -> None:
    """The lease cannot rely on httpx's per-operation inactivity timeout."""
    import src.services.arxiv.arxiv_service as svc

    acquire = AsyncMock(return_value=0.0)
    monkeypatch.setattr(svc, "_acquire_arxiv_rate_slot", acquire)

    with pytest.raises(svc.IngestionError, match="timed out"):
        await asyncio.wait_for(
            svc.request_arxiv_api(
                svc.ArXivIngestionService.ARXIV_API_BASE,
                {"search_query": "all:test"},
                timeout=0.01,
                max_attempts=1,
                client_factory=_SlowClient,
            ),
            timeout=0.2,
        )

    assert acquire.await_count == 1


async def test_429_reacquires_and_honours_full_retry_after(monkeypatch) -> None:
    import src.services.arxiv.arxiv_service as svc

    _Client.responses = [_Resp(429, retry_after=60), _Resp(200, text="ok")]
    acquire = AsyncMock(return_value=0.0)
    sleep = AsyncMock()
    monkeypatch.setattr(svc, "_acquire_arxiv_rate_slot", acquire)
    monkeypatch.setattr(svc.asyncio, "sleep", sleep)
    monkeypatch.setattr(svc, "_arm_arxiv_cooldown", AsyncMock())

    result = await svc.request_arxiv_api(
        svc.ArXivIngestionService.ARXIV_API_BASE,
        {"search_query": "all:test"},
        client_factory=_Client,
    )

    assert result == "ok"
    assert acquire.await_count == 2
    sleep.assert_any_await(60.0)
