"""Validate the deliberately narrow per-turn answer reasoning override.

This is a deployment preflight, not evidence that a live provider accepted a
request. Null preserves server defaults. Hosted boolean and model-specific
thinking modes have no inferred NOUS equivalent.
"""

import re
from datetime import date
from typing import Literal, cast

from src.core.config import get_settings
from src.core.openai_endpoint import classify_openai_endpoint

ReasoningEffort = Literal["low", "medium", "high"]
_EFFORTS = frozenset({"low", "medium", "high"})
_DEPLOYMENTS = frozenset({"gpt-5-mini", "gpt-5.6-luna"})


def validate_reasoning_override(
    value: object, *, model: str, document_only: bool
) -> ReasoningEffort | None:
    """Reject unsupported overrides before a user turn can be accepted."""
    if value is None:
        return None
    if not isinstance(value, str) or value not in _EFFORTS:
        raise ValueError("Reasoning override supports only low, medium, or high")
    if not document_only:
        raise ValueError("Reasoning override requires document-only execution")

    settings = get_settings()
    deployment = (
        model
        or settings.AZURE_OPENAI_CHAT_DEPLOYMENT_NAME
        or settings.AZURE_OPENAI_DEPLOYMENT_NAME
        or ""
    )
    if deployment not in _DEPLOYMENTS:
        raise ValueError("Reasoning override requires a supported NOUS GPT-5 model")
    if not settings.AGENT_USE_RESPONSES_API:
        raise ValueError("Reasoning override requires the Responses API")

    endpoint = settings.AZURE_OPENAI_CHAT_ENDPOINT or settings.AZURE_OPENAI_ENDPOINT
    if not endpoint:
        raise ValueError("Reasoning override requires a configured model endpoint")
    try:
        endpoint_kind = classify_openai_endpoint(endpoint)
    except ValueError as exc:
        raise ValueError("Reasoning override requires a valid model endpoint") from exc
    if endpoint_kind == "azure":
        version = (
            settings.AZURE_OPENAI_CHAT_API_VERSION or settings.AZURE_OPENAI_API_VERSION
        )
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-preview)?", str(version)):
                raise ValueError("Unrecognized API version")
            version_date = date.fromisoformat(str(version)[:10])
        except ValueError as exc:
            raise ValueError(
                "Reasoning override requires a supported Azure API version"
            ) from exc
        if version_date < date(2025, 4, 1):
            raise ValueError(
                "Reasoning override requires Azure API version 2025-04-01 or later"
            )
    return cast(ReasoningEffort, value)
