"""R7-M6: turn creation is one per-user budget, reads have their own.

/execute, /stream and the confirm endpoints each used to carry a private
30 rpm bucket, so rotating endpoints multiplied the real budget; /jobs,
/stream/resume and /stream/cancel carried none at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.agent import execute as execute_mod
from src.core.rate_limit import InMemoryRateLimiter

pytestmark = pytest.mark.unit

_TURN_BODY = {
    "messages": [{"role": "user", "content": "hi"}],
    "page_context": {"type": "chat"},
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from src.core.database import get_db
    from src.core.dependencies import get_current_user

    user = Mock()
    user.id = str(uuid4())
    user.organization_id = str(uuid4())
    user.is_active = True

    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=Mock(return_value=None))
    )
    db.rollback = AsyncMock()

    app = FastAPI()
    app.include_router(execute_mod.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    @asynccontextmanager
    async def _no_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app.router.lifespan_context = _no_lifespan
    with TestClient(app) as c:
        c.user_id = str(user.id)  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def turn_limiter(monkeypatch: pytest.MonkeyPatch) -> InMemoryRateLimiter:
    limiter = InMemoryRateLimiter(max_attempts=1, window_minutes=1)
    monkeypatch.setattr(execute_mod, "_agent_rate_limiter", limiter)
    return limiter


@pytest.fixture
def read_limiter(monkeypatch: pytest.MonkeyPatch) -> InMemoryRateLimiter:
    limiter = InMemoryRateLimiter(max_attempts=1, window_minutes=1)
    monkeypatch.setattr(execute_mod, "_agent_read_rate_limiter", limiter)
    return limiter


async def _spend_turn_budget(limiter: InMemoryRateLimiter, user_id: str) -> None:
    await limiter.record_attempt(user_id, prefix=execute_mod._AGENT_TURN_PREFIX)


@pytest.mark.parametrize("path", ["/api/v1/agent/execute", "/api/v1/agent/stream"])
async def test_execute_and_stream_share_one_turn_bucket(
    client: TestClient, turn_limiter: InMemoryRateLimiter, path: str
) -> None:
    """A turn spent on either endpoint counts against the other."""
    await _spend_turn_budget(turn_limiter, client.user_id)  # type: ignore[attr-defined]

    response = client.post(path, json=_TURN_BODY)

    assert (
        response.status_code == 429
    ), f"{path} must draw on the shared turn budget, not a private bucket"


async def test_confirm_shares_the_turn_bucket(
    client: TestClient, turn_limiter: InMemoryRateLimiter
) -> None:
    await _spend_turn_budget(turn_limiter, client.user_id)  # type: ignore[attr-defined]

    response = client.post(f"/api/v1/agent/confirm/{uuid4()}", json={"confirmed": True})

    assert response.status_code == 429


async def test_resume_is_rate_limited(
    client: TestClient, read_limiter: InMemoryRateLimiter
) -> None:
    """The resume endpoint had no limiter at all."""
    await read_limiter.record_attempt(
        client.user_id,  # type: ignore[attr-defined]
        prefix=execute_mod._AGENT_READ_PREFIX,
    )

    response = client.get(f"/api/v1/agent/stream/resume/{uuid4()}")

    assert response.status_code == 429


async def test_reads_do_not_consume_the_turn_budget(
    client: TestClient, turn_limiter: InMemoryRateLimiter
) -> None:
    """Polling must never lock a user out of starting their next turn."""
    client.get(f"/api/v1/agent/jobs/{uuid4()}")

    allowed, _ = await turn_limiter.check_rate_limit(
        client.user_id,  # type: ignore[attr-defined]
        prefix=execute_mod._AGENT_TURN_PREFIX,
    )
    assert allowed is True
