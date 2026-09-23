"""Unit tests for AzureOpenAIService chat completion methods."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.infrastructure.azure_openai_service import AzureOpenAIService


def _make_chunk(content: str | None) -> MagicMock:
    """Create a mock OpenAI streaming chunk with the given delta content."""
    chunk = MagicMock()
    choice = MagicMock()
    choice.delta.content = content
    chunk.choices = [choice]
    return chunk


def _make_empty_choices_chunk() -> MagicMock:
    """Create a mock chunk with an empty choices list (e.g. final chunk)."""
    chunk = MagicMock()
    chunk.choices = []
    return chunk


@pytest.fixture
def service() -> AzureOpenAIService:
    """Create an AzureOpenAIService with mocked clients so it doesn't hit real Azure."""
    with patch.object(AzureOpenAIService, "_initialize_clients"):
        svc = AzureOpenAIService()
        svc.client = MagicMock()
        svc.chat_client = None
        svc.async_client = None
        svc.embedding_client = None
    return svc


@pytest.mark.asyncio
async def test_yields_content_strings(service: AzureOpenAIService) -> None:
    """stream_chat_completion should yield each non-empty content string from chunks."""
    chunks = [
        _make_chunk("Hello"),
        _make_chunk(", "),
        _make_chunk("world!"),
    ]

    with patch.object(
        service,
        "chat_completion",
        new_callable=AsyncMock,
        return_value=iter(chunks),
    ):
        results = []
        async for token in service.stream_chat_completion(
            messages=[{"role": "user", "content": "Hi"}],
        ):
            results.append(token)

    assert results == ["Hello", ", ", "world!"]


@pytest.mark.asyncio
async def test_skips_empty_and_none_content(service: AzureOpenAIService) -> None:
    """stream_chat_completion should skip chunks with empty or None content."""
    chunks = [
        _make_chunk("Hello"),
        _make_chunk(None),
        _make_chunk(""),
        _make_chunk(" "),
        _make_chunk("world"),
        _make_empty_choices_chunk(),
    ]

    with patch.object(
        service,
        "chat_completion",
        new_callable=AsyncMock,
        return_value=iter(chunks),
    ):
        results = []
        async for token in service.stream_chat_completion(
            messages=[{"role": "user", "content": "Hi"}],
        ):
            results.append(token)

    # " " is a non-empty string and should be yielded; None and "" should be skipped.
    # The empty-choices chunk should also be skipped.
    assert results == ["Hello", " ", "world"]


@pytest.mark.asyncio
async def test_passes_stream_true(service: AzureOpenAIService) -> None:
    """stream_chat_completion must call chat_completion with stream=True."""
    chunks = [_make_chunk("ok")]

    mock_chat = AsyncMock(return_value=iter(chunks))
    with patch.object(service, "chat_completion", mock_chat):
        async for _ in service.stream_chat_completion(
            messages=[{"role": "user", "content": "Hi"}],
            temperature=0.5,
            max_tokens=100,
        ):
            pass

    mock_chat.assert_called_once_with(
        messages=[{"role": "user", "content": "Hi"}],
        temperature=0.5,
        max_tokens=100,
        stream=True,
    )


@pytest.mark.asyncio
async def test_forwards_default_parameters(service: AzureOpenAIService) -> None:
    """stream_chat_completion should forward temperature and max_tokens defaults."""
    chunks = [_make_chunk("x")]

    mock_chat = AsyncMock(return_value=iter(chunks))
    with patch.object(service, "chat_completion", mock_chat):
        async for _ in service.stream_chat_completion(
            messages=[{"role": "user", "content": "Hi"}],
        ):
            pass

    mock_chat.assert_called_once_with(
        messages=[{"role": "user", "content": "Hi"}],
        temperature=0.7,
        max_tokens=None,
        stream=True,
    )


@pytest.mark.asyncio
async def test_propagates_exception(service: AzureOpenAIService) -> None:
    """stream_chat_completion should propagate exceptions from chat_completion."""
    mock_chat = AsyncMock(side_effect=ValueError("client not initialized"))
    with patch.object(service, "chat_completion", mock_chat):
        with pytest.raises(ValueError, match="client not initialized"):
            async for _ in service.stream_chat_completion(
                messages=[{"role": "user", "content": "Hi"}],
            ):
                pass


@pytest.mark.asyncio
async def test_chat_completion_does_not_block_asyncio_deadlines(
    service: AzureOpenAIService,
) -> None:
    """A slow synchronous SDK call must yield so wait_for can cancel it."""
    response = MagicMock()
    response.choices = []

    def slow_create(**_kwargs):
        time.sleep(0.05)
        return response

    service.client.chat.completions.create.side_effect = slow_create
    with patch.object(service, "get_chat_deployment", return_value="gpt-4"):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                service.chat_completion(
                    messages=[{"role": "user", "content": "Hi"}],
                ),
                timeout=0.005,
            )


@pytest.mark.parametrize("deployment_name", ["gpt-4", "gpt-5"])
@pytest.mark.asyncio
async def test_chat_completion_bounds_provider_request_without_retries(
    service: AzureOpenAIService,
    deployment_name: str,
) -> None:
    """A caller timeout must constrain both SDK request branches."""
    async_client = MagicMock()
    scoped_client = MagicMock()
    scoped_client.chat.completions.create = AsyncMock(return_value=iter([]))
    async_client.with_options.return_value = scoped_client

    with (
        patch.object(service, "get_chat_deployment", return_value=deployment_name),
        patch.object(
            service,
            "_get_async_chat_client",
            return_value=async_client,
        ),
    ):
        await service.chat_completion(
            messages=[{"role": "user", "content": "Hi"}],
            stream=True,
            timeout=7.5,
        )

    request_options = async_client.with_options.call_args.kwargs
    assert 0 < request_options["timeout"] <= 7.5
    assert request_options["max_retries"] == 0
    scoped_client.chat.completions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_completion_timeout_cancels_inflight_provider_request(
    service: AzureOpenAIService,
) -> None:
    """The absolute budget must cancel the HTTP coroutine, not only its waiter."""
    request_cancelled = asyncio.Event()

    async def slow_create(**_kwargs):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            request_cancelled.set()
            raise

    async_client = MagicMock()
    scoped_client = MagicMock()
    scoped_client.chat.completions.create = AsyncMock(side_effect=slow_create)
    async_client.with_options.return_value = scoped_client

    with (
        patch.object(service, "get_chat_deployment", return_value="gpt-4"),
        patch.object(
            service,
            "_get_async_chat_client",
            return_value=async_client,
        ),
    ):
        with pytest.raises(asyncio.TimeoutError):
            await service.chat_completion(
                messages=[{"role": "user", "content": "Hi"}],
                stream=True,
                timeout=0.01,
            )

    assert request_cancelled.is_set()
