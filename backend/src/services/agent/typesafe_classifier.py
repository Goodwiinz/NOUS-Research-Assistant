"""Optional, deadline-bounded TypeSafe routing; Azure remains the fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any, Literal, cast

import httpx
from pydantic import BaseModel, Field, model_validator

from src.core.config import settings
from src.services.agent.classifier import (
    _CLASSIFIER_SYSTEM_PROMPT,
    ROUTING_RUBRIC_VERSION,
    ClassificationResult,
    IntentType,
    routing_criteria,
)
from src.services.agent.observability import record_token_usage

logger = logging.getLogger(__name__)
_client: httpx.AsyncClient | None = None
_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
RoutingChoice = Literal[
    "research", "writing", "knowledge_graph", "general", "unresolved"
]
Probability = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]


class ChoiceAnswer(BaseModel):
    type: Literal["choice"]
    choice: RoutingChoice
    confidence: Probability
    probabilities: dict[RoutingChoice, Probability]

    @model_validator(mode="after")
    def validate_distribution(self) -> ChoiceAnswer:
        expected = {"research", "writing", "knowledge_graph", "general", "unresolved"}
        if set(self.probabilities) != expected:
            raise ValueError("Incomplete routing distribution")
        # Five probabilities rounded to two decimals can drift by up to .025.
        if abs(sum(self.probabilities.values()) - 1.0) > 0.025001:
            raise ValueError("Invalid probability sum")
        if self.probabilities[self.choice] < max(self.probabilities.values()):
            raise ValueError("Choice is not a highest-probability option")
        return self


class Usage(BaseModel):
    input_tokens: int = Field(strict=True, ge=0)
    output_tokens: int = Field(strict=True, ge=0)


class RoutingResponse(BaseModel):
    model: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,80}$")
    answers: dict[str, ChoiceAnswer]
    usage: Usage


def start_typesafe_client() -> None:
    """Called from the application lifespan (or explicitly by the replay CLI)."""
    global _client
    if (
        _client is None
        and settings.AGENT_INTENT_PROVIDER == "typesafe"
        and settings.TYPESAFE_API_KEY
    ):
        _client = httpx.AsyncClient(follow_redirects=False)


async def close_typesafe_client() -> None:
    global _client
    client, _client = _client, None
    if client is not None:
        await client.aclose()


async def request_routing_decision(
    state: dict[str, Any], *, timeout: float
) -> RoutingResponse:
    """Validated raw judgment for the router and explicitly invoked offline evaluation."""
    if _client is None or not settings.TYPESAFE_API_KEY:
        raise RuntimeError("TypeSafe client is not initialized")
    response = await asyncio.wait_for(
        _client.post(
            _ENDPOINT,
            headers={
                "Authorization": f"Bearer {settings.TYPESAFE_API_KEY.get_secret_value()}"
            },
            json={
                "model": settings.TYPESAFE_MODEL,
                "state": state,
                "questions": {
                    "intent": {
                        "type": "choice",
                        "instructions": _CLASSIFIER_SYSTEM_PROMPT,
                        "criteria": routing_criteria(),
                    }
                },
            },
            timeout=timeout,
        ),
        timeout=timeout,
    )
    response.raise_for_status()
    if len(response.content) > 65536:
        raise ValueError("Oversized routing response")
    parsed = RoutingResponse.model_validate_json(response.content)
    if set(parsed.answers) != {"intent"}:
        raise ValueError("Unexpected answer keys")
    return cast(RoutingResponse, parsed)


async def classify_intent_typesafe(
    state: dict[str, Any], *, timeout: float
) -> ClassificationResult | None:
    """Return an accepted route or None; no retries and no cancellation swallowing."""
    threshold = settings.TYPESAFE_MIN_CONFIDENCE
    if threshold is None or not settings.TYPESAFE_API_KEY or _client is None:
        logger.info("TypeSafe routing unavailable or uncalibrated; using Azure")
        return None

    started = time.monotonic()
    outcome = "invalid_response"
    try:
        parsed = await request_routing_decision(state, timeout=timeout)
        answer = parsed.answers["intent"]
        record_token_usage(
            parsed.model, parsed.usage.input_tokens, parsed.usage.output_tokens
        )
        logger.info(
            "TypeSafe decision",
            extra={
                "classifier_model": parsed.model,
                "classifier_rubric": ROUTING_RUBRIC_VERSION,
                "classifier_choice": answer.choice,
                "classifier_confidence": answer.confidence,
            },
        )
        if answer.choice == "unresolved":
            outcome = "unresolved"
            return None
        if answer.confidence < threshold:
            outcome = "low_confidence"
            return None
        intent: IntentType = answer.choice
        outcome = "accepted"
        return ClassificationResult(
            intent=intent,
            confidence=answer.confidence,
            reasoning="TypeSafe route accepted by the configured confidence policy.",
            source="typesafe",
        )
    except (TimeoutError, httpx.TimeoutException):
        outcome = "timeout"
        return None
    except httpx.HTTPStatusError as exc:
        outcome = f"http_{exc.response.status_code}"
        return None
    except httpx.RequestError:
        outcome = "transport_error"
        return None
    except ValueError:
        # Pydantic errors include input values: do not log them or response bodies.
        return None
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        logger.info(
            "TypeSafe routing completed",
            extra={
                "classifier_outcome": outcome,
                "classifier_seconds": time.monotonic() - started,
                "classifier_rubric": ROUTING_RUBRIC_VERSION,
            },
        )
