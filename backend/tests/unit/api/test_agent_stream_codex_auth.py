"""Authorization boundary for browser-originated local Codex execution."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException

# Importing the agent router loads an unrelated document-processing task that
# initializes tiktoken from the network at import time.
with patch("tiktoken.get_encoding", return_value=Mock()):
    from src.api.agent import execute
from src.core.security import TokenData
from tests.utils.agent_stream import make_stream_request


@pytest.mark.asyncio
async def test_cli_token_cannot_start_codex_stream() -> None:
    request_body = make_stream_request(
        execution_provider="codex",
        thread_id=str(uuid4()),
        device_id=uuid4(),
        workspace_id=uuid4(),
    )
    request = SimpleNamespace(headers={})

    with pytest.raises(HTTPException) as exc_info:
        await execute.stream_agent(
            request_body,
            request,
            BackgroundTasks(),
            current_user=SimpleNamespace(id=uuid4()),
            token=TokenData(is_cli=True),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Interactive browser authentication required"


@pytest.mark.asyncio
async def test_cli_token_can_still_start_regular_nous_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = AsyncMock(return_value=None)
    monkeypatch.setattr(execute, "_enforce_rate_limit", generator)
    request_body = make_stream_request()

    response = await execute.stream_agent(
        request_body,
        SimpleNamespace(headers={}),
        BackgroundTasks(),
        current_user=SimpleNamespace(id=uuid4()),
        token=TokenData(is_cli=True),
    )

    assert response.status_code == 200
    generator.assert_awaited_once()
