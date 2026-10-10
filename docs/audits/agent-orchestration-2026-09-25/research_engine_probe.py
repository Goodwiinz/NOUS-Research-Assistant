"""Offline research-engine blueprint contract probe. No network or DB calls."""

# mypy: ignore-errors
# These probes intentionally use dynamic fixtures and optional YAML stubs;
# production modules remain covered by the normal type-checking gate.

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import yaml

from src.services.research_engine.connectors.base import SourceDocument
from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.providers.base import LLMResponse
from src.services.research_engine.step_executor import StepExecutor

ROOT = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[3]
TEMPLATES = REPO / "backend/src/services/research_engine/blueprints/templates"
CLAIM = "Accuracy improved by 20% in evaluation."


class Connector:
    def __init__(self, count=1):
        self.count = count

    async def search(self, query, max_results=50):
        return [
            SourceDocument(
                connector_type="arxiv",
                external_id=f"2401.{i:05d}",
                title=f"Paper {i}",
                abstract=CLAIM if self.count == 1 else "Evidence " * 125,
            )
            for i in range(self.count)
        ]


class Provider:
    def __init__(self, responses=None):
        self.requests = []
        self.responses = list(responses or [])

    async def complete(self, request):
        self.requests.append(request)
        return LLMResponse(
            content=self.responses.pop(0) if self.responses else CLAIM,
            model_id="mock",
            input_tokens=1,
            output_tokens=1,
        )


async def run_blueprint(bp, connector, provider, **kwargs):
    connectors = {
        name: connector
        for name in ["arxiv", "semantic_scholar", "pubmed", "web", "rag_store"]
    }
    engine = WorkflowEngine(StepExecutor(connectors, {"claude-sonnet-4-6": provider}))
    return [event async for event in engine.run(bp, uuid4(), **kwargs)]


async def main():
    for path in sorted(TEMPLATES.glob("*.yaml")):
        bp = yaml.safe_load(path.read_text())
        bp["parameters"]["query"] = "accuracy"
        provider = Provider()
        events = await run_blueprint(bp, Connector(), provider)
        verify_index = next(
            i for i, s in enumerate(bp["steps"]) if s["type"] == "verify"
        )
        verified = next(
            e
            for e in events
            if e["event"] == "step_complete" and e["step_index"] == verify_index
        )
        assert events[-1]["event"] == "run_paused", events
        assert verified["output"]["verified"] is False
        assert len(provider.requests) == sum(
            s["type"] in ["screen", "extract", "synthesize"] for s in bp["steps"]
        )
        assert any(CLAIM in request.prompt for request in provider.requests)
        # Mirror the SSE resume contract: persist all completed outputs, start after last step.
        context = {}
        for event in events:
            if event["event"] == "step_complete":
                context.update(event["output"])
        resumed = await run_blueprint(
            bp,
            Connector(),
            provider,
            start_from_step=verify_index + 1,
            initial_context=context,
        )
        assert resumed[-1]["event"] == "run_complete"
        assert resumed[-1]["context"]["verified"] is False
        print(
            json.dumps(
                {
                    "template": path.stem,
                    "terminal": events[-1]["event"],
                    "verify": verified["output"],
                    "model_calls": len(provider.requests),
                    "resume_terminal": resumed[-1]["event"],
                    "resume_verified": resumed[-1]["context"]["verified"],
                }
            )
        )

    bp = yaml.safe_load((TEMPLATES / "systematic_literature_review.yaml").read_text())
    provider = Provider()
    events = await run_blueprint(bp, Connector(count=10), provider)
    assert events[-1]["event"] == "run_failed"
    assert len(provider.requests) == 0
    assert "rendered LLM prompt exceeds" in events[-1]["error"]
    print(
        json.dumps(
            {
                "case": "10_papers_1125_char_abstracts",
                "terminal": events[-1]["event"],
                "error": events[-1]["error"],
                "model_calls": len(provider.requests),
            }
        )
    )

    # Malformed extraction bypasses the template's advertised schema validation
    # when an explicit matching evidence alias is supplied.
    bp = yaml.safe_load((TEMPLATES / "data_extraction.yaml").read_text())
    verify = next(s for s in bp["steps"] if s["type"] == "verify")
    malformed = "Accuracy improved by 20% in evaluation."
    result = await StepExecutor({}, {}).execute(
        verify,
        {
            "content": malformed,
            "source_text": malformed,
            "extraction_schema": {
                "type": "object",
                "required": ["sample_size"],
                "properties": {"sample_size": {"type": "integer"}},
            },
        },
    )
    assert result.output["verified"] is True
    print(
        json.dumps(
            {"case": "non_json_passes_schema_validation_step", "verify": result.output}
        )
    )


asyncio.run(main())
