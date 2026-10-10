from unittest.mock import patch

from src.services.evaluation.llm_judge_service import LLMJudgeService
from src.services.infrastructure.azure_openai_service import AzureOpenAIService


def _set_azure_settings(monkeypatch, **overrides):
    from src.services.infrastructure import azure_openai_service as azure_service_module

    defaults = {
        "AZURE_OPENAI_ENDPOINT": None,
        "AZURE_OPENAI_API_KEY": None,
        "AZURE_OPENAI_API_VERSION": "2024-02-15-preview",
        "AZURE_OPENAI_DEPLOYMENT_NAME": None,
        "AZURE_OPENAI_CHAT_ENDPOINT": None,
        "AZURE_OPENAI_CHAT_API_KEY": None,
        "AZURE_OPENAI_CHAT_API_VERSION": "2024-06-01",
        "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME": None,
        "AZURE_OPENAI_EMBEDDING_ENDPOINT": None,
        "AZURE_OPENAI_EMBEDDING_API_KEY": None,
        "AZURE_OPENAI_EMBEDDING_API_VERSION": "2023-05-15",
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME": None,
    }
    defaults.update(overrides)

    for key, value in defaults.items():
        monkeypatch.setattr(azure_service_module.settings, key, value, raising=False)


@patch("src.services.infrastructure.azure_openai_service.OpenAI")
@patch("src.services.infrastructure.azure_openai_service.AzureOpenAI")
def test_bare_services_ai_endpoint_uses_azure_client(
    mock_azure_openai,
    mock_openai,
    monkeypatch,
):
    _set_azure_settings(
        monkeypatch,
        AZURE_OPENAI_CHAT_ENDPOINT="https://example.services.ai.azure.com",
        AZURE_OPENAI_CHAT_API_KEY="chat-key",
    )

    AzureOpenAIService()

    mock_azure_openai.assert_called_once_with(
        api_key="chat-key",
        azure_endpoint="https://example.services.ai.azure.com",
        api_version="2024-06-01",
    )
    mock_openai.assert_not_called()


@patch("src.services.evaluation.llm_judge_service.OpenAI", create=True)
@patch("src.services.evaluation.llm_judge_service.AzureOpenAI")
def test_llm_judge_uses_openai_client_for_v1_base_url(
    mock_azure_openai,
    mock_openai,
):
    LLMJudgeService(
        endpoint="https://example.services.ai.azure.com/openai/v1/",
        api_key="judge-key",
        deployment_name="gpt-4o-mini",
    )

    mock_openai.assert_called_once_with(
        api_key="judge-key",
        base_url="https://example.services.ai.azure.com/openai/v1/",
    )
    mock_azure_openai.assert_not_called()


@patch("src.services.infrastructure.azure_openai_service.OpenAI")
@patch("src.services.infrastructure.azure_openai_service.AzureOpenAI")
def test_bare_cognitiveservices_endpoint_uses_azure_client(
    mock_azure_openai,
    mock_openai,
    monkeypatch,
):
    """Bare cognitiveservices hosts route through AzureOpenAI so deployment names
    are passed correctly (Azure SDK constructs the /deployments/<name>/... URL)."""
    _set_azure_settings(
        monkeypatch,
        AZURE_OPENAI_CHAT_ENDPOINT="https://example.cognitiveservices.azure.com",
        AZURE_OPENAI_CHAT_API_KEY="chat-key",
    )

    AzureOpenAIService()

    mock_azure_openai.assert_called_once_with(
        api_key="chat-key",
        azure_endpoint="https://example.cognitiveservices.azure.com",
        api_version="2024-06-01",
    )
    mock_openai.assert_not_called()


@patch("src.services.infrastructure.azure_openai_service.OpenAI")
@patch("src.services.infrastructure.azure_openai_service.AzureOpenAI")
def test_cognitiveservices_with_openai_v1_suffix_uses_openai_client(
    mock_azure_openai,
    mock_openai,
    monkeypatch,
):
    """cognitiveservices hosts are allowed when suffixed with /openai/v1 —
    they route through the OpenAI-compatible client (which handles this URL
    pattern natively, sidestepping the Azure SDK quirks that motivate the
    bare-host rejection).
    """
    _set_azure_settings(
        monkeypatch,
        AZURE_OPENAI_CHAT_ENDPOINT="https://example.cognitiveservices.azure.com/openai/v1",
        AZURE_OPENAI_CHAT_API_KEY="chat-key",
    )

    AzureOpenAIService()

    mock_openai.assert_called_once()
    mock_azure_openai.assert_not_called()


@patch("src.services.infrastructure.azure_openai_service.OpenAI")
@patch("src.services.infrastructure.azure_openai_service.AzureOpenAI")
def test_model_router_endpoint_uses_openai_compatible_client(
    mock_azure_openai,
    mock_openai,
    monkeypatch,
):
    """The Foundry `model-router` deployment lives at
    `goodwiinzapix.cognitiveservices.azure.com/openai/v1/` (note the trailing
    slash). It must route through the OpenAI-compatible client with
    `base_url=` set to the full v1 URL — the deployment name is passed as the
    `model=` parameter at call time, not in the URL.
    """
    _set_azure_settings(
        monkeypatch,
        AZURE_OPENAI_CHAT_ENDPOINT="https://goodwiinzapix.cognitiveservices.azure.com/openai/v1/",
        AZURE_OPENAI_CHAT_API_KEY="router-key",
        AZURE_OPENAI_CHAT_DEPLOYMENT_NAME="model-router",
        AZURE_OPENAI_CHAT_API_VERSION="2024-12-01-preview",
    )

    AzureOpenAIService()

    mock_openai.assert_called_once_with(
        api_key="router-key",
        base_url="https://goodwiinzapix.cognitiveservices.azure.com/openai/v1/",
    )
    mock_azure_openai.assert_not_called()


@patch("src.services.infrastructure.azure_openai_service.AsyncOpenAI")
@patch("src.services.infrastructure.azure_openai_service.AsyncAzureOpenAI")
@patch("src.services.infrastructure.azure_openai_service.OpenAI")
@patch("src.services.infrastructure.azure_openai_service.AzureOpenAI")
def test_timed_chat_lazily_uses_matching_async_azure_client(
    mock_azure_openai,
    _mock_openai,
    mock_async_azure_openai,
    mock_async_openai,
    monkeypatch,
):
    _set_azure_settings(
        monkeypatch,
        AZURE_OPENAI_CHAT_ENDPOINT="https://example.services.ai.azure.com",
        AZURE_OPENAI_CHAT_API_KEY="chat-key",
    )
    service = AzureOpenAIService()

    async_client = service._get_async_chat_client()

    assert async_client is mock_async_azure_openai.return_value
    mock_async_azure_openai.assert_called_once_with(
        api_key="chat-key",
        azure_endpoint="https://example.services.ai.azure.com",
        api_version="2024-06-01",
    )
    mock_async_openai.assert_not_called()
    mock_azure_openai.assert_called_once()
