"""
Azure OpenAI Service Integration
Provides support for Azure OpenAI models alongside existing OpenAI and Anthropic integrations
"""

import asyncio
import logging
import os
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import tiktoken
from openai import AzureOpenAI, OpenAI

from src.core.config import settings
from src.core.openai_endpoint import classify_openai_endpoint

logger = logging.getLogger(__name__)


class AzureOpenAIService:
    """Azure OpenAI service for chat completions and embeddings"""

    def __init__(self):
        self.client = None
        self.embedding_client = None
        self.chat_client = None
        self._initialize_clients()

    def _initialize_clients(self):
        """Initialize Azure OpenAI clients with separate endpoints for chat and embeddings"""
        try:
            # Initialize chat client with chat endpoint and API key
            chat_endpoint = (
                settings.AZURE_OPENAI_CHAT_ENDPOINT or settings.AZURE_OPENAI_ENDPOINT
            )
            chat_api_key = (
                settings.AZURE_OPENAI_CHAT_API_KEY or settings.AZURE_OPENAI_API_KEY
            )

            if chat_endpoint and chat_api_key:
                if classify_openai_endpoint(chat_endpoint) == "openai_compatible":
                    self.client = OpenAI(
                        api_key=chat_api_key,
                        base_url=chat_endpoint,
                    )
                    logger.info(
                        f"Chat client initialized (OpenAI-compat) with endpoint: {chat_endpoint}"
                    )
                else:
                    self.client = AzureOpenAI(
                        api_key=chat_api_key,
                        azure_endpoint=chat_endpoint,
                        api_version=settings.AZURE_OPENAI_CHAT_API_VERSION,
                    )
                    logger.info(
                        f"Chat client initialized (Azure) with endpoint: {chat_endpoint}"
                    )

            # Initialize embedding client with embedding endpoint and API key
            embedding_endpoint = (
                settings.AZURE_OPENAI_EMBEDDING_ENDPOINT
                or settings.AZURE_OPENAI_ENDPOINT
            )
            embedding_api_key = (
                settings.AZURE_OPENAI_EMBEDDING_API_KEY or settings.AZURE_OPENAI_API_KEY
            )

            if embedding_endpoint and embedding_api_key:
                if classify_openai_endpoint(embedding_endpoint) == "openai_compatible":
                    self.embedding_client = OpenAI(
                        api_key=embedding_api_key,
                        base_url=embedding_endpoint,
                    )
                    logger.info(
                        f"Embedding client initialized (OpenAI-compat) with endpoint: {embedding_endpoint}"
                    )
                else:
                    self.embedding_client = AzureOpenAI(
                        api_key=embedding_api_key,
                        azure_endpoint=embedding_endpoint,
                        api_version=settings.AZURE_OPENAI_EMBEDDING_API_VERSION,
                    )
                    logger.info(
                        f"Embedding client initialized (Azure) with endpoint: {embedding_endpoint}"
                    )

            # For backwards compatibility, also initialize a general client
            if settings.AZURE_OPENAI_ENDPOINT and settings.AZURE_OPENAI_API_KEY:
                if (
                    classify_openai_endpoint(settings.AZURE_OPENAI_ENDPOINT)
                    == "openai_compatible"
                ):
                    self.chat_client = OpenAI(
                        api_key=settings.AZURE_OPENAI_API_KEY,
                        base_url=settings.AZURE_OPENAI_ENDPOINT,
                    )
                else:
                    self.chat_client = AzureOpenAI(
                        api_key=settings.AZURE_OPENAI_API_KEY,
                        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
                        api_version=settings.AZURE_OPENAI_API_VERSION,
                    )
                logger.info(
                    f"General client initialized with endpoint: {settings.AZURE_OPENAI_ENDPOINT}"
                )

            logger.info("Azure OpenAI clients initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize Azure OpenAI clients: {str(e)}")
            raise

    def is_available(self) -> bool:
        """Check if Azure OpenAI service is available"""
        return (
            self.client is not None
            or self.embedding_client is not None
            or self.chat_client is not None
        )

    def is_chat_available(self) -> bool:
        """Check if Azure OpenAI chat service is available"""
        return self.client is not None or self.chat_client is not None

    def is_embedding_available(self) -> bool:
        """Check if Azure OpenAI embedding service is available"""
        return self.embedding_client is not None

    def get_embedding_deployment(self) -> Optional[str]:
        """Get the embedding deployment name"""
        return (
            settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME or "text-embedding-ada-002"
        )

    def get_chat_deployment(self) -> Optional[str]:
        """Get the chat deployment name"""
        return (
            settings.AZURE_OPENAI_CHAT_DEPLOYMENT_NAME
            or settings.AZURE_OPENAI_DEPLOYMENT_NAME
        )

    async def get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """
        Get embeddings for a list of texts using Azure OpenAI

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors
        """
        if not self.embedding_client:
            raise ValueError("Azure OpenAI embedding client not initialized")

        deployment_name = self.get_embedding_deployment()
        if not deployment_name:
            raise ValueError("Azure OpenAI embedding deployment name not configured")

        try:
            embeddings = []
            for text in texts:
                response = self.embedding_client.embeddings.create(
                    input=[text],
                    model=deployment_name,  # In Azure OpenAI, this is the deployment name
                )
                embeddings.append(response.data[0].embedding)

            return embeddings

        except Exception as e:
            logger.error(f"Error getting embeddings from Azure OpenAI: {str(e)}")
            raise

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Get chat completion from Azure OpenAI

        Args:
            messages: List of message dictionaries with 'role' and 'content'
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            stream: Whether to stream the response
            timeout: Optional provider request budget in seconds

        Returns:
            Chat completion response
        """
        # Use the appropriate client (prefer chat-specific client, fallback to general client)
        chat_client = self.client or self.chat_client
        if not chat_client:
            raise ValueError("Azure OpenAI chat client not initialized")

        deployment_name = self.get_chat_deployment()
        if not deployment_name:
            raise ValueError("Azure OpenAI chat deployment name not configured")

        if timeout is not None and timeout <= 0:
            raise asyncio.TimeoutError

        request_deadline = time.monotonic() + timeout if timeout is not None else None

        def create_completion(**kwargs: Any) -> Any:
            request_client = chat_client
            if request_deadline is not None:
                remaining_timeout = request_deadline - time.monotonic()
                if remaining_timeout <= 0:
                    raise asyncio.TimeoutError
                request_client = chat_client.with_options(
                    timeout=remaining_timeout,
                    max_retries=0,
                )
            return request_client.chat.completions.create(**kwargs)

        try:
            # Handle different parameter names for newer models
            if deployment_name and "gpt-5" in deployment_name.lower():
                # GPT-5 models use max_completion_tokens and temperature must be 1.0
                response = await asyncio.to_thread(
                    create_completion,
                    model=deployment_name,
                    messages=messages,
                    temperature=1.0,  # GPT-5 Nano only supports temperature=1.0
                    max_completion_tokens=max_tokens,
                    stream=stream,
                )
            else:
                # Older models use max_tokens
                kwargs = dict(
                    model=deployment_name,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=stream,
                )
                if tools:
                    kwargs["tools"] = tools
                response = await asyncio.to_thread(create_completion, **kwargs)

            if stream:
                return response  # Return streaming response
            else:
                choice = response.choices[0]
                result = {
                    "content": choice.message.content,
                    "usage": {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                    },
                    "model": deployment_name,
                    "finish_reason": choice.finish_reason,
                }
                # Include tool calls if present
                if choice.message.tool_calls:
                    result["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in choice.message.tool_calls
                    ]
                return result

        except Exception as e:
            logger.error(f"Error getting chat completion from Azure OpenAI: {str(e)}")
            raise

    async def stream_chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Async generator that streams chat completion tokens from Azure OpenAI.

        Calls the existing chat_completion() method with stream=True and yields
        each non-empty content string from the streamed chunks.

        Args:
            messages: List of message dictionaries with 'role' and 'content'
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Yields:
            Individual content strings from the streaming response
        """
        response = await self.chat_completion(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )

        for chunk in response:
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content
            if content:
                yield content

    def count_tokens(self, text: str, model: str = "gpt-4") -> int:
        """
        Count tokens for text using tiktoken

        Args:
            text: Text to count tokens for
            model: Model name for tokenization

        Returns:
            Number of tokens
        """
        try:
            # Use cl100k_base encoding (compatible with GPT-4 and GPT-3.5-turbo)
            encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text))
        except Exception as e:
            logger.error(f"Error counting tokens: {str(e)}")
            # Fallback: rough estimate (1 token ≈ 4 characters for English)
            return len(text) // 4

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about configured Azure OpenAI models"""
        return {
            "provider": "Azure OpenAI",
            "endpoint": settings.AZURE_OPENAI_ENDPOINT,
            "api_version": settings.AZURE_OPENAI_API_VERSION,
            "chat_deployment": self.get_chat_deployment(),
            "embedding_deployment": self.get_embedding_deployment(),
            "available": self.is_available(),
        }


# Global instance
azure_openai_service = AzureOpenAIService()
