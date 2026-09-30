"""Shared LLM factory for lightweight agent auxiliary tasks.

Centralises the build logic for classifier, compactor, reflection,
planner, and memory-store LLMs so the deployment name is configured
once via ``AZURE_OPENAI_LIGHTWEIGHT_DEPLOYMENT`` (env/settings).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from langchain_core.language_models import BaseChatModel

from src.core.config import get_settings
from src.core.openai_endpoint import classify_openai_endpoint

logger = logging.getLogger(__name__)


def _main_chat_deployment() -> str:
    """The configured chat deployment — same chain ``graph._build_llm`` uses."""
    settings = get_settings()
    return (
        settings.AZURE_OPENAI_CHAT_DEPLOYMENT_NAME
        or settings.AZURE_OPENAI_DEPLOYMENT_NAME
        or ""
    )


def resolve_chat_deployment(model_override: str | None = None) -> str:
    """Resolve the deployment selected by ``graph._build_llm`` exactly."""
    return model_override or _main_chat_deployment()


def credential_fingerprint() -> str:
    """Short digest of the endpoint + API key the chat builders will use.

    Belongs in every LLM cache key. Credentials are settings-derived and so
    were assumed stable, but they are exactly the settings that DO change
    without a restart: an Infisical sync can rotate `AZURE_OPENAI_CHAT_API_KEY`
    while the pods keep running. A cache keyed without them then hands back a
    client authenticating with the dead key on every subsequent call, and no
    amount of retrying fixes it. Including the digest makes a rotation miss the
    cache and rebuild, which is the desired behaviour and costs one hash per
    build. The value is truncated and never logged.
    """
    settings = get_settings()
    endpoint = (
        settings.AZURE_OPENAI_CHAT_ENDPOINT or settings.AZURE_OPENAI_ENDPOINT or ""
    )
    api_key = settings.AZURE_OPENAI_CHAT_API_KEY or settings.AZURE_OPENAI_API_KEY or ""
    return hashlib.sha256(f"{endpoint}:{api_key}".encode()).hexdigest()[:12]


# Cache built chat models keyed by the builder's input args. Constructing a
# ChatOpenAI/AzureChatOpenAI spins up an HTTP client (~30-50ms); the synthesis
# path previously rebuilt one on every turn. deployment / reasoning_effort /
# max_retries / resolved request_timeout are settings-derived and stable at
# runtime, so they need not be part of the key — but the CREDENTIALS are not
# (see `credential_fingerprint`), so every key below carries their digest.
_LIGHTWEIGHT_LLM_CACHE: dict[tuple, BaseChatModel] = {}
_SYNTHESIS_LLM_CACHE: dict[tuple, BaseChatModel] = {}
_FAST_PATH_LLM_CACHE: dict[tuple, BaseChatModel] = {}


def reset_llm_caches() -> None:
    """Clear the factory's per-args LLM caches.

    For tests that exercise the real builders and need a fresh instance.
    """
    _LIGHTWEIGHT_LLM_CACHE.clear()
    _SYNTHESIS_LLM_CACHE.clear()
    _FAST_PATH_LLM_CACHE.clear()


def _resolve_lightweight_deployment() -> str:
    """Lightweight deployment, falling back to the main chat deployment.

    The fallback used to be ``model-router``, which meant an unset or blank
    secret silently put the router back inside the agent loop — the exact
    configuration that produced the 30s timeout cap on research_llm_node
    (trace 019e1da5). Falling back to the configured chat deployment keeps a
    misconfigured environment on the model the operator actually chose, and
    makes "one deployment everywhere" the natural result of leaving the
    per-role overrides unset.
    """
    settings = get_settings()
    return settings.AZURE_OPENAI_LIGHTWEIGHT_DEPLOYMENT or _main_chat_deployment()


def _resolve_synthesis_deployment() -> str:
    """Synthesis deploy falls back to lightweight, then the main deployment."""
    settings = get_settings()
    return (
        settings.AZURE_OPENAI_SYNTHESIS_DEPLOYMENT
        or settings.AZURE_OPENAI_LIGHTWEIGHT_DEPLOYMENT
        or _main_chat_deployment()
    )


def _resolve_fast_path_deployment() -> str:
    """Dedicated deployment for evidence-independent direct streaming."""
    return get_settings().AGENT_FAST_PATH_DEPLOYMENT or "gpt-5.6-luna"


def build_fast_path_llm() -> BaseChatModel:
    """Return the cached direct-streaming model for evidence-independent turns."""
    settings = get_settings()
    deployment = _resolve_fast_path_deployment()
    cache_key = (
        deployment,
        settings.AGENT_FAST_PATH_MAX_OUTPUT_TOKENS,
        settings.AGENT_FAST_PATH_REQUEST_TIMEOUT,
        credential_fingerprint(),
    )
    if cache_key not in _FAST_PATH_LLM_CACHE:
        _FAST_PATH_LLM_CACHE[cache_key] = _build_chat_llm(
            deployment,
            role_label="Fast-path",
            max_tokens=settings.AGENT_FAST_PATH_MAX_OUTPUT_TOKENS,
            streaming=True,
            request_timeout=settings.AGENT_FAST_PATH_REQUEST_TIMEOUT,
            reasoning_effort="none",
            max_retries=1,
        )
    return _FAST_PATH_LLM_CACHE[cache_key]


def _build_chat_llm(
    deployment: str,
    *,
    role_label: str,
    temperature: float = 0,
    max_tokens: int = 512,
    streaming: bool = False,
    request_timeout: float | None = None,
    use_responses_api: bool | None = None,
    reasoning_effort: str | None = None,
    max_retries: int | None = None,
) -> BaseChatModel:
    """Shared core builder for lightweight + synthesis LLMs.

    Keeps the gpt-5 family quirks (no temperature, reasoning_effort)
    and Azure vs OpenAI-compatible endpoint selection in one place.
    """
    settings = get_settings()

    endpoint = (
        settings.AZURE_OPENAI_CHAT_ENDPOINT or settings.AZURE_OPENAI_ENDPOINT or ""
    )
    api_key = settings.AZURE_OPENAI_CHAT_API_KEY or settings.AZURE_OPENAI_API_KEY or ""
    api_version = (
        settings.AZURE_OPENAI_CHAT_API_VERSION or settings.AZURE_OPENAI_API_VERSION
    )
    logger.info("%s LLM deployment resolved to: %s", role_label, deployment)

    if not endpoint or not api_key:
        raise RuntimeError(
            "Azure/OpenAI chat endpoint and API key must be configured. "
            "Set AZURE_OPENAI_CHAT_ENDPOINT + AZURE_OPENAI_CHAT_API_KEY "
            "(or the non-CHAT variants)."
        )

    # All gpt-5 family deployments (gpt-5, gpt-5-mini, gpt-5-nano, etc.)
    # reject the `temperature` parameter — Azure returns 400. Skip it
    # rather than maintaining an explicit allowlist as new sizes ship.
    _accepts_temperature = not deployment.startswith("gpt-5")

    # Bound LLM call wall-clock + cap retries. Lightweight callers (classifier,
    # planner, reflection) get a shorter ceiling than the main agent so a stuck
    # model-router call cannot block the whole turn.
    if request_timeout is None:
        request_timeout = settings.AGENT_LIGHTWEIGHT_REQUEST_TIMEOUT
    if max_retries is None:
        max_retries = settings.AGENT_LLM_MAX_RETRIES

    extra: dict[str, Any] = {
        "request_timeout": request_timeout,
        "max_retries": max_retries,
        "streaming": streaming,
    }
    if _accepts_temperature:
        extra["temperature"] = temperature
    # Default to Chat Completions API. langchain-openai auto-routes gpt-5
    # family + reasoning_effort to Azure Responses API, which currently
    # rejects multi-part / tool_call messages with "Unsupported data type".
    # Callers can opt back in by passing use_responses_api=True explicitly.
    extra["use_responses_api"] = (
        False if use_responses_api is None else use_responses_api
    )

    # gpt-5 family supports reasoning_effort. Lightweight tasks (classifier,
    # reflection, planner complexity check) default to "minimal".
    if deployment.startswith("gpt-5") and reasoning_effort:
        extra["reasoning_effort"] = reasoning_effort

    if classify_openai_endpoint(endpoint) == "openai_compatible":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=deployment,
            api_key=api_key,
            base_url=endpoint,
            max_tokens=max_tokens,
            **extra,
        )

    from langchain_openai import AzureChatOpenAI

    return AzureChatOpenAI(
        azure_deployment=deployment,
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=api_version,
        max_tokens=max_tokens,
        **extra,
    )


def _reasoning_effort_for(configured: str | None, *, tool_calling: bool) -> str | None:
    """Resolve ``reasoning_effort``, dropping it on tool-calling turns.

    Azure rejects the combination on Chat Completions:

        400 "Function tools with reasoning_effort are not supported for this
             model in /v1/chat/completions. Please use /v1/responses instead."

    This guards the **Chat Completions** path, which is what we send today.
    ``_build_llm`` in graph.py dropped the kwarg outright for the same reason
    (#1334); this covers the auxiliary builders, whose results also reach
    ``bind_tools`` / ``with_structured_output(method="function_calling")``.

    The Responses API *is* an escape, contrary to what #1334 assumed. Measured
    2026-08-03 against gpt-5.6-luna: with ``api_version="2025-04-01-preview"``
    (the configured ``2024-12-01-preview`` is too old and 400s with "Responses
    API is enabled only for api-version…"), ``use_responses_api=True`` accepts
    tools plus ``reasoning_effort`` all the way to ``max``, including a full
    HumanMessage/AIMessage-with-tool_calls/ToolMessage history. The
    "Unsupported data type" failure that pinned ``use_responses_api=False`` was
    the stale api-version, not the message shape.

    Switching is still not a flag flip: on the Responses path ``content`` comes
    back as typed blocks (``[{"type": "text", ...}]``, plus ``rs_…`` reasoning
    items at xhigh/max) where the streaming and persistence layers expect a
    string. Bumping the api-version and flipping the flag is the real fix for
    keeping reasoning on tool-calling turns — tracked separately.

    ponytail: a flag rather than sniffing the model — "which models allow it"
    is Azure-side state we cannot read. If we move to Responses, delete this
    function and pass ``configured`` straight through.
    """
    return None if tool_calling else (configured or None)


def build_lightweight_llm(
    *,
    temperature: float = 0,
    max_tokens: int = 512,
    request_timeout: float | None = None,
    use_responses_api: bool | None = None,
    tool_calling: bool = False,
) -> BaseChatModel:
    """Build a LangChain chat model for lightweight auxiliary tasks.

    Targets the deployment configured via
    ``AZURE_OPENAI_LIGHTWEIGHT_DEPLOYMENT``, falling back to the main chat
    deployment.

    Most callers here (classifier, reflection, compactor, memory insights) send
    no function tools. The ones that do — anything reaching ``bind_tools`` or
    ``with_structured_output(..., method="function_calling")`` — must pass
    ``tool_calling=True`` so ``reasoning_effort`` is dropped; see
    ``_reasoning_effort_for``.
    """
    key = (
        temperature,
        max_tokens,
        request_timeout,
        use_responses_api,
        tool_calling,
        credential_fingerprint(),
    )
    cached = _LIGHTWEIGHT_LLM_CACHE.get(key)
    if cached is not None:
        return cached
    settings = get_settings()
    llm = _build_chat_llm(
        _resolve_lightweight_deployment(),
        role_label="Lightweight",
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=False,
        request_timeout=request_timeout,
        use_responses_api=use_responses_api,
        reasoning_effort=_reasoning_effort_for(
            settings.AGENT_LIGHTWEIGHT_REASONING_EFFORT, tool_calling=tool_calling
        ),
    )
    _LIGHTWEIGHT_LLM_CACHE[key] = llm
    return llm


def build_synthesis_llm(
    *,
    temperature: float = 0,
    max_tokens: int = 4096,
    request_timeout: float | None = None,
    use_responses_api: bool | None = None,
    tool_calling: bool = False,
) -> BaseChatModel:
    """Build a LangChain chat model for post-tool prose synthesis.

    Targets ``AZURE_OPENAI_SYNTHESIS_DEPLOYMENT``, falling back to the
    lightweight deployment and then to the main chat deployment, so existing
    single-knob deployments keep working and leaving every override unset
    runs one deployment everywhere. Set it only to make prose cheaper than
    the tool-decision path — not the other way round.
    """
    key = (
        temperature,
        max_tokens,
        request_timeout,
        use_responses_api,
        tool_calling,
        credential_fingerprint(),
    )
    cached = _SYNTHESIS_LLM_CACHE.get(key)
    if cached is not None:
        return cached
    settings = get_settings()
    # Synthesis call can be long (full 4096-token completion). Use the main
    # agent timeout, not the lightweight one, unless caller overrides.
    resolved_timeout = (
        request_timeout
        if request_timeout is not None
        else settings.AGENT_LLM_REQUEST_TIMEOUT
    )
    llm = _build_chat_llm(
        _resolve_synthesis_deployment(),
        role_label="Synthesis",
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=True,
        request_timeout=resolved_timeout,
        use_responses_api=use_responses_api,
        # Defaults to the lightweight tier's effort — most callers here are
        # prose or a short classification. AGENT_SYNTHESIS_REASONING_EFFORT
        # decouples the two when synthesis needs to think harder than a
        # classifier; unset keeps the previous single-knob behaviour.
        #
        # Not every caller is prose-only, though: the post-tool synthesis
        # turns in llm_node and the three subgraphs bind the intent's tool
        # set to this model so the loop can still take another step. Those
        # pass tool_calling=True and give up the effort setting — Azure
        # rejects tools + reasoning_effort. See _reasoning_effort_for.
        # Raising the knob does nothing for those four until
        # AGENT_USE_RESPONSES_API is on.
        reasoning_effort=_reasoning_effort_for(
            settings.AGENT_SYNTHESIS_REASONING_EFFORT
            or settings.AGENT_LIGHTWEIGHT_REASONING_EFFORT,
            tool_calling=tool_calling,
        ),
    )
    _SYNTHESIS_LLM_CACHE[key] = llm
    return llm


def get_lightweight_model_name() -> str:
    """Return the configured lightweight model/deployment name."""
    return _resolve_lightweight_deployment()


def get_synthesis_model_name() -> str:
    """Return the configured synthesis model/deployment name."""
    return _resolve_synthesis_deployment()


def validate_llm_config() -> bool:
    """True if an Azure/OpenAI chat endpoint AND api key resolve. Used at
    startup to fail readiness rather than crash individual turns."""
    s = get_settings()
    endpoint = s.AZURE_OPENAI_CHAT_ENDPOINT or s.AZURE_OPENAI_ENDPOINT or ""
    api_key = s.AZURE_OPENAI_CHAT_API_KEY or s.AZURE_OPENAI_API_KEY or ""
    return bool(endpoint and api_key)
