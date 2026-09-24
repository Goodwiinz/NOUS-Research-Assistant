"""Step executor for research engine workflow steps."""

import copy
import hashlib
import re
from dataclasses import dataclass, field
from string import Template
from typing import Any, Dict, List, Optional

from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.discovery import search_sources, source_records
from src.services.research_engine.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
)
from src.services.research_engine.verification import (
    QualityMark,
    run_source_grounding_check,
)

MAX_CONNECTOR_FANOUT = 4
MAX_CONNECTOR_RESULTS = 50
MAX_RENDERED_PROMPT_CHARS = 16 * 1024


def _safe_render(template_str: str, context: Dict[str, Any]) -> str:
    """Render template strings safely.

    Supports both `$variable` and `{variable}` placeholders while avoiding
    arbitrary expression evaluation.
    """
    if not template_str:
        return ""

    rendered = Template(template_str).safe_substitute(context)

    # Support legacy `{variable}` placeholders in YAML templates.
    brace_pattern = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in context:
            return str(context[key])
        return match.group(0)

    return brace_pattern.sub(repl, rendered)


@dataclass
class StepResult:
    """Result of executing a single workflow step."""

    output: Dict[str, Any]
    sources_used: List[SourceDocument] = field(default_factory=list)
    quality_marks: List[QualityMark] = field(default_factory=list)
    token_count: int = 0
    inputs_hash: Optional[str] = None
    outputs_hash: Optional[str] = None
    full_prompt: Optional[str] = None


class StepExecutor:
    """Dispatches and executes individual workflow steps."""

    def __init__(
        self,
        connectors: Dict[str, SourceConnector],
        providers: Dict[str, LLMProvider],
    ) -> None:
        self.connectors = connectors
        self.providers = providers
        self._handlers = {
            "search": self._execute_search,
            "screen": self._execute_screen,
            "extract": self._execute_extract,
            "synthesize": self._execute_synthesize,
            "export": self._execute_export,
            "verify": self._execute_verify,
        }

    async def execute(self, step_def: Dict, context: Dict) -> StepResult:
        """Execute a step based on its type, dispatching to the appropriate handler."""
        step_type = step_def.get("type", "")
        handler = self._handlers.get(step_type)
        if handler is None:
            raise ValueError(f"Unknown step type: {step_type}")
        return await handler(step_def, context)

    def _get_params(self, step_def: Dict) -> Dict:
        """Get step parameters, checking both 'params' and 'parameters' keys."""
        return step_def.get("params") or step_def.get("parameters") or {}

    async def _execute_search(self, step_def: Dict, context: Dict) -> StepResult:
        """Search across configured source connectors."""
        params = self._get_params(step_def)
        sources = params.get("sources")
        if sources is None:
            sources = (
                [params["source"]]
                if params.get("source")
                else context.get("selected_sources", context.get("sources", []))
            )
        if not isinstance(sources, list) or not all(
            isinstance(name, str) for name in sources
        ):
            raise ValueError("sources must be a list of provider names")
        if len(sources) > MAX_CONNECTOR_FANOUT:
            raise ValueError(
                f"connector fanout exceeds the {MAX_CONNECTOR_FANOUT}-connector limit"
            )
        query_template = params.get("query_template", "$query")
        try:
            max_results = int(params.get("max_results", MAX_CONNECTOR_RESULTS))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_results must be an integer") from exc
        if max_results < 1 or max_results > MAX_CONNECTOR_RESULTS:
            raise ValueError(
                f"connector results exceed the {MAX_CONNECTOR_RESULTS}-result limit"
            )
        query = _safe_render(query_template, context)
        if len(query) > MAX_RENDERED_PROMPT_CHARS:
            raise ValueError("rendered connector query exceeds the server limit")

        all_sources, coverage = await search_sources(
            self.connectors, sources, query, max_results
        )

        return StepResult(
            output={
                "sources": [s.title for s in all_sources],
                "query": query,
                "source_records": source_records(all_sources),
                "coverage": coverage,
                "selected_sources": sources,
            },
            sources_used=all_sources,
        )

    async def _execute_screen(self, step_def: Dict, context: Dict) -> StepResult:
        return await self._execute_llm_step(step_def, context)

    async def _execute_extract(self, step_def: Dict, context: Dict) -> StepResult:
        return await self._execute_llm_step(step_def, context)

    async def _execute_synthesize(self, step_def: Dict, context: Dict) -> StepResult:
        return await self._execute_llm_step(step_def, context)

    async def _execute_export(self, step_def: Dict, context: Dict) -> StepResult:
        params = self._get_params(step_def)
        export_fields = params.get("fields")
        if isinstance(export_fields, list) and export_fields:
            exported = {
                field_name: context.get(field_name) for field_name in export_fields
            }
        else:
            exported = dict(context)

        return StepResult(
            output={
                "exported": exported,
                "format": params.get("format", "json"),
            }
        )

    async def _execute_verify(self, step_def: Dict, context: Dict) -> StepResult:
        """Verification step with deterministic source-grounding quality mark."""
        claim = str(context.get("claim") or context.get("content") or "")
        source_text = str(
            context.get("source_text")
            or context.get("evidence")
            or context.get("context")
            or ""
        )
        quality_mark = run_source_grounding_check(claim, source_text)

        return StepResult(
            output={
                "verified": quality_mark.passed,
                "check": quality_mark.check_type,
                "details": quality_mark.details,
            },
            quality_marks=[quality_mark],
        )

    async def _execute_llm_step(self, step_def: Dict, context: Dict) -> StepResult:
        """Execute a step that requires LLM completion."""
        params = self._get_params(step_def)
        model_id = step_def.get("model_id") or params.get("model_id", "")
        system_prompt_template = step_def.get("system_prompt_template") or params.get(
            "system_prompt_template", ""
        )
        temperature = float(step_def.get("temperature", params.get("temperature", 0.0)))
        seed = step_def.get("seed", params.get("seed", 42))

        provider = self.providers.get(model_id)
        if provider is None:
            raise ValueError(f"No provider found for model_id: {model_id}")

        # Retrieval audit fields identify a particular observation, not evidence
        # content. Keep them persisted, but do not perturb reproducible prompts.
        prompt_context = copy.deepcopy(context)
        coverage = prompt_context.get("coverage")
        if isinstance(coverage, dict):
            coverage.pop("retrieved_at", None)
        for record in prompt_context.get("source_records", []):
            record.pop("source_id", None)
            for snapshot in record.get("metadata", {}).get("provenance", []):
                snapshot.pop("retrieved_at", None)
        system_prompt = _safe_render(system_prompt_template, prompt_context)
        if len(system_prompt) > MAX_RENDERED_PROMPT_CHARS:
            raise ValueError("rendered system prompt exceeds the server limit")

        prompt = str(prompt_context)
        if len(prompt.encode("utf-8")) > MAX_RENDERED_PROMPT_CHARS:
            raise ValueError("rendered LLM prompt exceeds the server limit")

        request = LLMRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            seed=seed,
        )

        response: LLMResponse = await provider.complete(request)

        inputs_hash = hashlib.sha256(
            (request.prompt + (request.system_prompt or "")).encode()
        ).hexdigest()
        outputs_hash = hashlib.sha256(response.content.encode()).hexdigest()

        return StepResult(
            output={"content": response.content},
            token_count=response.total_tokens,
            inputs_hash=inputs_hash,
            outputs_hash=outputs_hash,
            full_prompt=system_prompt,
        )
