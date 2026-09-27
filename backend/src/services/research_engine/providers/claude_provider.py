"""Anthropic Claude LLM provider."""

import structlog

from src.services.research_engine.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderConfig,
)

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

logger = structlog.get_logger(__name__)


class ClaudeProvider(LLMProvider):
    """LLM provider for Anthropic Claude models."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        if anthropic is not None and config.api_key:
            self.client = anthropic.AsyncAnthropic(api_key=config.api_key)
        else:
            self.client = None  # type: ignore[assignment]

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send a completion request to the Claude API."""
        if self.client is None:
            raise RuntimeError(
                "Anthropic client not initialized. "
                "Ensure the 'anthropic' package is installed and an API key is set."
            )

        kwargs = {
            "model": self.config.model_id,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [{"role": "user", "content": request.prompt}],
        }

        if request.system_prompt:
            kwargs["system"] = request.system_prompt

        logger.debug(
            "claude_completion_request",
            model=self.config.model_id,
            temperature=request.temperature,
        )

        response = await self.client.messages.create(**kwargs)

        return LLMResponse(
            content=response.content[0].text,
            model_id=response.model,
            model_version=self.config.model_version,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            temperature=request.temperature,
            seed=None,
        )

    async def is_model_available(self) -> bool:
        """Check availability by verifying an API key is configured."""
        return self.config.api_key is not None
