"""Explicitly invoked routing-only replay; no agent tools or production mutations.

The bundled cases are synthetic regressions derived from previously seen examples,
NOT an independent holdout. Default invocation validates the dataset offline.
Use --run-live to send minimized case context to the selected provider(s).
Output is JSONL without queries, context, secrets or raw exception bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.core.config import settings
from src.services.agent.classifier import (
    _CLASSIFIER_SYSTEM_PROMPT,
    ROUTING_RUBRIC_VERSION,
    build_routing_state,
    classify_intent_with_fallback,
    routing_criteria,
)
from src.services.agent.tools import TOOL_REGISTRY
from src.services.agent.typesafe_classifier import (
    RoutingChoice,
    close_typesafe_client,
    request_routing_decision,
    start_typesafe_client,
)


class RoutingCase(BaseModel):
    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    page_context: dict[str, Any] = Field(default_factory=dict)
    previous_turn: str = ""
    prior_tool: dict[str, Any] | None = None
    acceptable_intents: list[RoutingChoice] = Field(min_length=1)
    required_tools: list[str] = Field(default_factory=list)
    unsupported: bool = False


def branch_tools() -> dict[str, set[str]]:
    return {
        intent: {
            tool.name
            for tool in (
                TOOL_REGISTRY.descriptors_for_intent(intent)
                if intent == "general"
                else TOOL_REGISTRY.descriptors_for_subgraph(
                    "data" if intent == "knowledge_graph" else intent
                )
            )
        }
        for intent in ("research", "writing", "knowledge_graph", "general")
    }


def load_cases(path: Path) -> list[RoutingCase]:
    cases = [RoutingCase.model_validate(row) for row in json.loads(path.read_text())]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("Cases must be nonempty with unique IDs")
    tools = branch_tools()
    for case in cases:
        feasible = {
            intent
            for intent, names in tools.items()
            if set(case.required_tools) <= names
        }
        if case.unsupported:
            if feasible or case.acceptable_intents != ["unresolved"]:
                raise ValueError(f"Unsupported-case contract drift: {case.id}")
        elif not set(case.acceptable_intents) <= feasible:
            raise ValueError(f"Expected route lacks required tools: {case.id}")
    return cases


async def replay(cases: list[RoutingCase], provider: str) -> None:
    settings.AGENT_INTENT_PROVIDER = "azure" if provider == "azure" else "typesafe"
    start_typesafe_client()
    tools = branch_tools()
    rubric_hash = hashlib.sha256(
        json.dumps(
            {"instructions": _CLASSIFIER_SYSTEM_PROMPT, "criteria": routing_criteria()},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    try:
        for case in cases:
            state = build_routing_state(
                case.query, case.page_context, case.previous_turn, case.prior_tool
            )
            row: dict[str, Any] = {
                "id": case.id,
                "provider": provider,
                "rubric": ROUTING_RUBRIC_VERSION,
                "rubric_sha256": rubric_hash,
                "unsupported": case.unsupported,
                "case_sha256": hashlib.sha256(
                    case.model_dump_json().encode()
                ).hexdigest(),
                "configured_typesafe_model": settings.TYPESAFE_MODEL,
                "typesafe_min_confidence": settings.TYPESAFE_MIN_CONFIDENCE,
            }
            started = time.monotonic()
            try:
                if provider == "typesafe":
                    raw = await request_routing_decision(
                        state, timeout=settings.TYPESAFE_TIMEOUT_SECONDS
                    )
                    answer = raw.answers["intent"]
                    intent = answer.choice
                    row.update(answer.model_dump())
                    row.update(model=raw.model, usage=raw.usage.model_dump())
                else:
                    result = await classify_intent_with_fallback(
                        case.query,
                        case.page_context,
                        case.previous_turn,
                        case.prior_tool,
                    )
                    intent = result.intent
                    row.update(
                        choice=intent,
                        confidence=result.confidence,
                        source=result.source,
                    )
                row["acceptable_route"] = intent in case.acceptable_intents
                row["tool_coverage"] = (
                    None
                    if case.unsupported
                    else set(case.required_tools) <= tools.get(intent, set())
                )
            except Exception as exc:
                row.update(
                    error_type=type(exc).__name__,
                    acceptable_route=False,
                    tool_coverage=None,
                )
            row["seconds"] = time.monotonic() - started
            print(json.dumps(row), flush=True)
    finally:
        await close_typesafe_client()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).with_name("intent_routing_cases.json"),
    )
    parser.add_argument(
        "--provider", choices=("azure", "typesafe", "cascade"), default="azure"
    )
    parser.add_argument(
        "--run-live",
        action="store_true",
        help="Allow paid routing calls for this dataset only",
    )
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if not args.run_live:
        print(
            json.dumps(
                {
                    "validated_cases": len(cases),
                    "network_calls": 0,
                    "independent_holdout": False,
                }
            )
        )
        return
    if args.provider != "azure" and not settings.TYPESAFE_API_KEY:
        parser.error(
            "TYPESAFE_API_KEY must be supplied through the environment/secret manager"
        )
    if args.provider == "cascade" and settings.TYPESAFE_MIN_CONFIDENCE is None:
        parser.error(
            "Set a calibration threshold for cascade replay; do not infer one from demo results"
        )
    if args.provider != "typesafe":
        from src.services.agent.llm_factory import validate_llm_config

        if not validate_llm_config():
            parser.error("Azure/OpenAI routing credentials are unavailable")
    asyncio.run(replay(cases, args.provider))


if __name__ == "__main__":
    main()
